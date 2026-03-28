"""
SC Toolbox update checker — queries the GitHub Releases API to detect new versions.
"""

from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from typing import Callable, Optional, Tuple

import urllib.request
import json

log = logging.getLogger(__name__)

GITHUB_REPO = "ScPlaceholder/SC-Toolbox"
RELEASES_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
TAGS_URL = f"https://api.github.com/repos/{GITHUB_REPO}/tags"
REPO_URL = f"https://github.com/{GITHUB_REPO}"

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@dataclass
class UpdateResult:
    available: bool
    latest_version: str
    current_version: str
    release_url: str
    error: str = ""


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
                    # version = "1.2.0"
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
    except Exception:
        return None


def check_for_updates() -> UpdateResult:
    """Synchronous update check — call from a background thread.

    Strategy:
      1. Try the releases/latest endpoint (ideal once the repo publishes releases).
      2. Fall back to the tags endpoint (looks for semver-like tags).
      3. If neither yields a version, report "up to date" (no false alarms).
    """
    current = get_current_version()
    releases_url = f"{REPO_URL}/releases"

    # ── Attempt 1: GitHub Releases ──
    data = _github_get(RELEASES_URL)
    if data and isinstance(data, dict):
        tag = data.get("tag_name", "")
        release_url = data.get("html_url", releases_url)
        if tag:
            return _compare(tag, current, release_url)

    # ── Attempt 2: Tags ──
    tags = _github_get(TAGS_URL)
    if tags and isinstance(tags, list) and len(tags) > 0:
        # Find the first tag that looks like a version (e.g. v1.3.0, 1.3.0)
        for t in tags:
            name = t.get("name", "")
            parsed = _parse_version(name)
            if parsed != (0,):
                tag_url = f"{REPO_URL}/releases/tag/{name}"
                return _compare(name, current, tag_url)

    # ── No release info available yet ──
    log.info("No releases or version tags found on %s — skipping update check", GITHUB_REPO)
    return UpdateResult(False, current, current, releases_url)


def _compare(remote_tag: str, current: str, release_url: str) -> UpdateResult:
    latest_tuple = _parse_version(remote_tag)
    current_tuple = _parse_version(current)
    return UpdateResult(
        available=latest_tuple > current_tuple,
        latest_version=remote_tag.lstrip("vV"),
        current_version=current,
        release_url=release_url,
    )


def check_for_updates_async(callback: Callable[[UpdateResult], None]) -> None:
    """Run the update check on a background thread; invoke *callback* with the result."""
    def _worker():
        result = check_for_updates()
        callback(result)
    t = threading.Thread(target=_worker, daemon=True)
    t.start()
