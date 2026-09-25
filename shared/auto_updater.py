"""
SC Toolbox auto-updater — download and apply a release ZIP over the install root.
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
import urllib.request
import zipfile

log = logging.getLogger(__name__)

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Files/dirs to never overwrite during an update
_SKIP_SUFFIXES = ("_config.json", "_settings.json")
_SKIP_FILES = {"skill_launcher_settings.json"}
_SKIP_DIRS = {"__pycache__", ".pytest_cache", "tests", "logs"}
_SKIP_EXT = {".log"}


def _should_skip(rel_path: str) -> bool:
    """Return True if *rel_path* should be preserved during apply_zip."""
    # Normalise to forward slashes for consistent checks
    rel = rel_path.replace("\\", "/")
    parts = rel.split("/")

    # Skip if any path component is a protected directory
    for part in parts[:-1]:  # all but the filename
        if part in _SKIP_DIRS:
            return True

    filename = parts[-1] if parts else ""

    # Skip protected filenames
    if filename in _SKIP_FILES:
        return True

    # Skip by suffix
    for suffix in _SKIP_SUFFIXES:
        if filename.endswith(suffix):
            return True

    # Skip log files
    _, ext = os.path.splitext(filename)
    if ext.lower() in _SKIP_EXT:
        return True

    return False


def is_installer(url: str) -> bool:
    """True when the release asset is the Windows setup .exe, not a zip.

    Every release since the installer existed ships SC_Toolbox_Setup_<ver>.exe,
    and update_checker prefers that asset. The UI used to hand it to apply_zip,
    which cannot open an .exe, so the UPDATE NOW button always failed."""
    return url.lower().split("?", 1)[0].endswith(".exe")


def run_installer(path: str) -> None:
    """Start the downloaded setup .exe detached, so it outlives the launcher.

    The caller must quit the launcher right after, so the installer can replace
    its files. The installer's own wizard is shown (no silent flags)."""
    import subprocess
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    subprocess.Popen([path], creationflags=flags, close_fds=True)


def download(url: str, on_progress, cancel: threading.Event, suffix: str = ".zip") -> str:
    """Download *url* to a temp file and return its path.

    *on_progress(bytes_done, total)* is called periodically; *total* may be 0
    if the server does not send Content-Length.  Raises InterruptedError if
    *cancel* is set before the download finishes.
    """
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "SC-Toolbox-AutoUpdater"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        total = int(resp.headers.get("Content-Length", 0) or 0)
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=suffix, prefix="sc_toolbox_update_")
        try:
            done = 0
            chunk = 65536  # 64 KiB
            with os.fdopen(tmp_fd, "wb") as fh:
                tmp_fd = None  # ownership transferred to fh
                while True:
                    if cancel.is_set():
                        raise InterruptedError("Download cancelled by user")
                    buf = resp.read(chunk)
                    if not buf:
                        break
                    fh.write(buf)
                    done += len(buf)
                    try:
                        on_progress(done, total)
                    except Exception:
                        pass
        except Exception:
            if tmp_fd is not None:
                try:
                    os.close(tmp_fd)
                except OSError:
                    pass
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
    return tmp_path


def apply_zip(zip_path: str, root: str = _PROJECT_ROOT) -> int:
    """Extract *zip_path* over *root*, skipping user config/log files.

    GitHub-style archives have a single top-level directory prefix
    (e.g. ``SC-Toolbox-abc123/``); this prefix is stripped automatically.

    Returns the count of files actually written.
    """
    updated = 0
    with zipfile.ZipFile(zip_path, "r") as zf:
        names = zf.namelist()

        # Detect and strip a single top-level directory prefix
        prefix = ""
        if names:
            first = names[0]
            # If the first entry looks like a directory (ends with /) use it
            if first.endswith("/"):
                candidate = first
            else:
                # Check whether ALL entries share the same first path component
                candidate = first.split("/")[0] + "/"
            if all(n.startswith(candidate) or n == candidate.rstrip("/") for n in names):
                prefix = candidate

        for member in names:
            # Strip the top-level prefix
            rel = member[len(prefix):] if prefix and member.startswith(prefix) else member

            # Skip the directory entry itself
            if not rel or rel.endswith("/"):
                continue

            if _should_skip(rel):
                log.debug("Skipping protected file: %s", rel)
                continue

            dest = os.path.normpath(os.path.join(root, rel.replace("/", os.sep)))

            # Safety: never write outside the install root
            if not dest.startswith(os.path.normpath(root)):
                log.warning("Skipping out-of-root path: %s", dest)
                continue

            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with zf.open(member) as src, open(dest, "wb") as dst:
                dst.write(src.read())
            updated += 1

    return updated
