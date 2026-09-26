"""Local inventory persistence — tracks which blueprints the user owns.

Data stored at ~/.sctoolbox/mission_db/inventory.json (survives app updates).
Supports a folder/subfolder tree for organizing owned blueprints.
"""

from __future__ import annotations

import json
import logging
import os
import threading

log = logging.getLogger(__name__)

_NEW_DIR = os.path.join(os.path.expanduser("~"), ".sctoolbox", "mission_db")
_NEW_INV_PATH = os.path.join(_NEW_DIR, "inventory.json")

# Legacy path (wiped on app updates) — used for migration only
_OLD_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".mission_db_cache")
_OLD_INV_PATH = os.path.join(_OLD_CACHE_DIR, "inventory.json")

_ROOT = "/"


def blueprint_key(bp: dict) -> str:
    """Return a stable identifier for a blueprint dict."""
    tag = bp.get("tag") or ""
    pec = bp.get("productEntityClass") or ""
    if tag and pec:
        return f"{tag}|{pec}"
    # ``or ""`` not ``.get(..., "")``: productName is PRESENT but null on 5
    # of 1,607 4.10.1 blueprints, and a None key poisons the inventory dict.
    return tag or pec or (bp.get("productName") or "")


def _parent_path(folder_path: str) -> str:
    """Return the parent folder path. '/' for top-level folders."""
    if folder_path == _ROOT:
        return _ROOT
    parent = folder_path.rsplit("/", 1)[0]
    return parent if parent else _ROOT


def _sanitize_name(name: str) -> str:
    """Remove / and strip whitespace from a folder name."""
    return name.replace("/", "").strip()


class InventoryService:
    """Thread-safe store of owned blueprint data + folder tree, persisted as JSON."""

    def __init__(self, path: str | None = None) -> None:
        self._path = path or _NEW_INV_PATH
        self._lock = threading.Lock()
        self._owned: dict[str, dict] = {}
        self._folders: dict[str, dict] = {}
        self._load()

    # ── Load / save / migration ───────────────────────────────────────────

    def _load(self) -> None:
        data = self._try_read(self._path)

        # Migration: if new path empty, try old path
        if data is None:
            old_data = self._try_read(_OLD_INV_PATH)
            if old_data is not None:
                log.info("Migrating inventory from legacy path: %s", _OLD_INV_PATH)
                data = old_data

        if data is None:
            self._owned = {}
            self._folders = {_ROOT: {"name": "Root", "children": [], "blueprints": []}}
            return

        self._owned = data.get("owned", {})
        version = data.get("version", 1)

        if version < 2 or "folders" not in data:
            # v1 → v2: put all blueprints in root
            self._folders = {
                _ROOT: {
                    "name": "Root",
                    "children": [],
                    "blueprints": list(self._owned.keys()),
                }
            }
        else:
            self._folders = data["folders"]

        # Ensure root always exists
        if _ROOT not in self._folders:
            self._folders[_ROOT] = {"name": "Root", "children": [], "blueprints": []}

        # Orphan detection: any bp in owned but not referenced by any folder
        all_assigned: set[str] = set()
        for fdata in self._folders.values():
            all_assigned.update(fdata.get("blueprints", []))
        orphans = set(self._owned.keys()) - all_assigned
        if orphans:
            log.info("Assigning %d orphaned blueprints to root", len(orphans))
            self._folders[_ROOT]["blueprints"].extend(sorted(orphans))

        # Save to new location (persists migration + orphan fixes)
        if version < 2 or self._path != _NEW_INV_PATH:
            self._path = _NEW_INV_PATH
            self._save_snapshot()

        log.debug("Inventory loaded: %d blueprints, %d folders",
                  len(self._owned), len(self._folders))

    @staticmethod
    def _try_read(path: str) -> dict | None:
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            return None
        except (json.JSONDecodeError, OSError):
            log.warning("Inventory file corrupted: %s", path)
            return None

    def _save_snapshot(self) -> None:
        """Write current state to disk (called with lock NOT held)."""
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        payload = {
            "version": 2,
            "owned": self._owned,
            "folders": self._folders,
        }
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)

    def _save(self) -> None:
        """Snapshot under lock, then write."""
        # Caller must hold self._lock — we copy and release before I/O
        owned_copy = dict(self._owned)
        folders_copy = json.loads(json.dumps(self._folders))
        # Release lock before I/O by using the copies
        payload = {"version": 2, "owned": owned_copy, "folders": folders_copy}
        os.makedirs(os.path.dirname(self._path), exist_ok=True)
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)

    # ── Blueprint CRUD ────────────────────────────────────────────────────

    def is_owned(self, bp_id: str) -> bool:
        with self._lock:
            return bp_id in self._owned

    def owned_ids(self) -> set[str]:
        with self._lock:
            return set(self._owned.keys())

    def owned_count(self) -> int:
        with self._lock:
            return len(self._owned)

    def add(self, bp_id: str, bp_dict: dict, folder: str = _ROOT) -> None:
        with self._lock:
            self._owned[bp_id] = bp_dict
            # Add to folder if not already referenced anywhere
            found = any(bp_id in f.get("blueprints", [])
                        for f in self._folders.values())
            if not found:
                target = folder if folder in self._folders else _ROOT
                self._folders[target].setdefault("blueprints", []).append(bp_id)
            self._save()

    def remove(self, bp_id: str) -> None:
        with self._lock:
            self._owned.pop(bp_id, None)
            for fdata in self._folders.values():
                bps = fdata.get("blueprints", [])
                if bp_id in bps:
                    bps.remove(bp_id)
            self._save()

    def get_all(self) -> list[dict]:
        with self._lock:
            return list(self._owned.values())

    # ── Folder CRUD ───────────────────────────────────────────────────────

    def create_folder(self, parent: str, name: str) -> str:
        """Create a subfolder. Returns the new folder's path."""
        name = _sanitize_name(name)
        if not name:
            raise ValueError("Folder name cannot be empty")
        with self._lock:
            if parent not in self._folders:
                raise ValueError(f"Parent folder not found: {parent}")
            if name in self._folders[parent].get("children", []):
                raise ValueError(f"Folder '{name}' already exists in {parent}")
            new_path = f"{parent.rstrip('/')}/{name}" if parent != _ROOT else f"/{name}"
            self._folders[parent].setdefault("children", []).append(name)
            self._folders[new_path] = {"name": name, "children": [], "blueprints": []}
            self._save()
        return new_path

    def rename_folder(self, folder_path: str, new_name: str) -> str:
        """Rename a folder. Returns the new path."""
        new_name = _sanitize_name(new_name)
        if not new_name:
            raise ValueError("Folder name cannot be empty")
        if folder_path == _ROOT:
            raise ValueError("Cannot rename root folder")
        with self._lock:
            if folder_path not in self._folders:
                raise ValueError(f"Folder not found: {folder_path}")
            parent = _parent_path(folder_path)
            old_name = self._folders[folder_path]["name"]
            if new_name == old_name:
                self._save()
                return folder_path
            # Check for conflict
            siblings = self._folders[parent].get("children", [])
            if new_name in siblings:
                raise ValueError(f"Folder '{new_name}' already exists in {parent}")
            # Update parent's children list
            idx = siblings.index(old_name)
            siblings[idx] = new_name
            # Build new path
            new_path = f"{parent.rstrip('/')}/{new_name}" if parent != _ROOT else f"/{new_name}"
            # Rename in folder registry — must also rename all descendant paths
            self._rename_subtree(folder_path, new_path, new_name)
            self._save()
        return new_path

    def _rename_subtree(self, old_path: str, new_path: str, new_name: str) -> None:
        """Recursively rename folder_path and all descendants (lock must be held)."""
        fdata = self._folders.pop(old_path)
        fdata["name"] = new_name
        self._folders[new_path] = fdata
        for child_name in list(fdata.get("children", [])):
            old_child = f"{old_path.rstrip('/')}/{child_name}"
            new_child = f"{new_path.rstrip('/')}/{child_name}"
            if old_child in self._folders:
                self._rename_subtree(old_child, new_child, child_name)

    def delete_folder(self, folder_path: str) -> None:
        """Delete a folder, moving its blueprints and sub-folders to parent."""
        if folder_path == _ROOT:
            raise ValueError("Cannot delete root folder")
        with self._lock:
            if folder_path not in self._folders:
                raise ValueError(f"Folder not found: {folder_path}")
            parent = _parent_path(folder_path)
            folder_name = self._folders[folder_path]["name"]
            # Collect all blueprints from this folder and descendants
            rescued_bps: list[str] = []
            rescued_children: list[str] = []
            self._collect_and_remove(folder_path, rescued_bps, rescued_children)
            # Move rescued blueprints to parent
            self._folders[parent]["blueprints"].extend(rescued_bps)
            # Remove from parent's children list
            children = self._folders[parent].get("children", [])
            if folder_name in children:
                children.remove(folder_name)
            self._save()

    def _collect_and_remove(self, path: str, bps: list, children: list) -> None:
        """Recursively collect blueprints and remove folder entries (lock held)."""
        fdata = self._folders.pop(path, {})
        bps.extend(fdata.get("blueprints", []))
        for child_name in fdata.get("children", []):
            child_path = f"{path.rstrip('/')}/{child_name}"
            self._collect_and_remove(child_path, bps, children)

    def move_blueprint(self, bp_id: str, target_folder: str) -> None:
        """Move a blueprint from its current folder to target_folder."""
        with self._lock:
            if target_folder not in self._folders:
                raise ValueError(f"Target folder not found: {target_folder}")
            # Remove from current location
            for fdata in self._folders.values():
                bps = fdata.get("blueprints", [])
                if bp_id in bps:
                    bps.remove(bp_id)
            # Add to target
            self._folders[target_folder].setdefault("blueprints", []).append(bp_id)
            self._save()

    def move_blueprints(self, bp_ids: list[str], target_folder: str) -> None:
        """Batch move multiple blueprints to target_folder."""
        with self._lock:
            if target_folder not in self._folders:
                raise ValueError(f"Target folder not found: {target_folder}")
            id_set = set(bp_ids)
            for fdata in self._folders.values():
                bps = fdata.get("blueprints", [])
                fdata["blueprints"] = [b for b in bps if b not in id_set]
            self._folders[target_folder].setdefault("blueprints", []).extend(bp_ids)
            self._save()

    # ── Folder queries ────────────────────────────────────────────────────

    def get_folder_contents(self, folder_path: str) -> tuple[list[str], list[dict]]:
        """Return (subfolder_names, blueprint_dicts) for the given folder."""
        with self._lock:
            fdata = self._folders.get(folder_path, {})
            children = list(fdata.get("children", []))
            bp_ids = fdata.get("blueprints", [])
            bps = [self._owned[bid] for bid in bp_ids if bid in self._owned]
            return children, bps

    def get_folder_bp_ids(self, folder_path: str) -> list[str]:
        """Return the blueprint IDs in the given folder (not recursive)."""
        with self._lock:
            fdata = self._folders.get(folder_path, {})
            return list(fdata.get("blueprints", []))

    def get_folder_tree(self) -> dict:
        """Return a deep copy of the folder tree."""
        with self._lock:
            return json.loads(json.dumps(self._folders))

    def folder_exists(self, folder_path: str) -> bool:
        with self._lock:
            return folder_path in self._folders

    def get_blueprint_folder(self, bp_id: str) -> str:
        """Return the folder path containing this blueprint, or '/'."""
        with self._lock:
            for path, fdata in self._folders.items():
                if bp_id in fdata.get("blueprints", []):
                    return path
            return _ROOT

    def folder_blueprint_count(self, folder_path: str, recursive: bool = False) -> int:
        with self._lock:
            return self._count_bps(folder_path, recursive)

    def _count_bps(self, path: str, recursive: bool) -> int:
        fdata = self._folders.get(path, {})
        count = len(fdata.get("blueprints", []))
        if recursive:
            for child_name in fdata.get("children", []):
                child_path = f"{path.rstrip('/')}/{child_name}"
                count += self._count_bps(child_path, True)
        return count

    def child_folder_path(self, parent: str, child_name: str) -> str:
        """Build the path for a child folder given parent and name."""
        if parent == _ROOT:
            return f"/{child_name}"
        return f"{parent}/{child_name}"
