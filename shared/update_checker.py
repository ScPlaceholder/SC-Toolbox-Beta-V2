"""
SC Toolbox update checker — queries the GitHub Releases API to detect new versions.

Version detection strategy (highest wins):
  1. Version parsed from the .exe asset filename  (SC_Toolbox_Setup_2.1.0.exe → 2.1.0)
  2. Version from the release tag_name            (v2.0.0 → 2.0.0)
  3. Fall back to tags endpoint

This means uploading a new installer with a higher version number in its
filename is enough to trigger an update notification — no new tag required.
"""

from __future__ import annotations

import logging
import os
import re
import threading
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

import urllib.request
import json

log = logging.getLogger(__name__)

GITHUB_REPO  = "ScPlaceholder/SC-Toolbox-Beta-V2"
RELEASES_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
TAGS_URL     = f"https://api.github.com/repos/{GITHUB_REPO}/tags"
REPO_URL     = f"https://github.com/{GITHUB_REPO}"

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Matches version numbers inside installer filenames, e.g. SC_Toolbox_Setup_2.1.0.exe
_ASSET_VER_RE = re.compile(r"(\d+\.\d+(?:\.\d+)*)")


@dataclass
class UpdateResult:
    available: bool
    latest_version: str
    current_version: str
    release_url: str
    error: str = ""
    download_url: str = ""


def _parse_version(v: str) -> Tuple[int, ...]:
    """Strip leading 'v' and parse into a comparable tuple."""
    v = v.strip().lstrip("vV")
    parts = []
    for p in v.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            break
    return tuple(parts) or (0,)


def get_current_version() -> str:
    """Read the version string from pyproject.toml."""
    toml_path = os.path.join(_PROJECT_ROOT, "pyproject.toml")
    try:
        with open(toml_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("version"):
                    _, _, val = line.partition("=")
                    return val.strip().strip('"').strip("'")
    except OSError:
        pass
    return "0.0.0"


def _github_get(url: str) -> Optional[object]:
    """Perform a GitHub API GET request; return parsed JSON or None on error."""
    try:
        req = urllib.request.Request(
            url,
            headers={"Accept": "application/vnd.github+json", "User-Agent": "SC-Toolbox"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except Exception as exc:
        log.warning("update_checker: GitHub API request failed (%s): %s", url, exc)
        return None


def _version_from_asset_name(name: str) -> Tuple[int, ...]:
    """Extract a version tuple from an asset filename, e.g. 'SC_Toolbox_Setup_2.1.0.exe'."""
    m = _ASSET_VER_RE.search(name)
    return _parse_version(m.group(1)) if m else (0,)


def check_for_updates() -> UpdateResult:
    """Synchronous update check — call from a background thread."""
    current = get_current_version()
    current_tuple = _parse_version(current)
    releases_url = f"{REPO_URL}/releases"

    # ── Attempt 1: GitHub Releases ──
    data = _github_get(RELEASES_URL)
    if data and isinstance(data, dict):
        tag = data.get("tag_name", "")
        release_url = data.get("html_url", releases_url)

        # Scan all .exe assets and pick the one with the highest version number
        dl_url = ""
        asset_version_tuple: Tuple[int, ...] = (0,)
        asset_version_str = ""
        for asset in data.get("assets", []):
            if not isinstance(asset, dict):
                continue
            name = asset.get("name", "")
            if not name.lower().endswith(".exe"):
                continue
            ver = _version_from_asset_name(name)
            if ver > asset_version_tuple:
                asset_version_tuple = ver
                dl_url = asset.get("browser_download_url", "")
                if ver != (0,):
                    asset_version_str = ".".join(str(x) for x in ver)

        if not dl_url:
            dl_url = data.get("zipball_url", "")

        # Use whichever version is higher: asset filename or tag
        tag_tuple = _parse_version(tag) if tag else (0,)
        if asset_version_tuple > tag_tuple:
            latest_tuple = asset_version_tuple
            latest_str   = asset_version_str
        else:
            latest_tuple = tag_tuple
            latest_str   = tag.lstrip("vV") if tag else ""

        if latest_str:
            return UpdateResult(
                available        = latest_tuple > current_tuple,
                latest_version   = latest_str,
                current_version  = current,
                release_url      = release_url,
                download_url     = dl_url,
            )

    # ── Attempt 2: Tags ──
    tags = _github_get(TAGS_URL)
    if tags and isinstance(tags, list):
        for t in tags:
            name = t.get("name", "")
            parsed = _parse_version(name)
            if parsed != (0,):
                tag_url = f"{REPO_URL}/releases/tag/{name}"
                return UpdateResult(
                    available       = parsed > current_tuple,
                    latest_version  = name.lstrip("vV"),
                    current_version = current,
                    release_url     = tag_url,
                )

    # ── No release info available ──
    log.info("update_checker: no releases or version tags found on %s", GITHUB_REPO)
    return UpdateResult(False, current, current, releases_url)


def check_for_updates_async(callback: Callable[[UpdateResult], None]) -> None:
    """Run the update check on a background thread; invoke *callback* with the result."""
    def _worker():
        result = check_for_updates()
        callback(result)
    t = threading.Thread(target=_worker, daemon=True, name="UpdateChecker")
    t.start()
