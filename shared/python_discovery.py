"""
Unified Python discovery — single source of truth for finding a system Python
with tkinter support.  Caches the result so repeated calls are free.
"""
import logging
import os
import shutil
import subprocess
import sys
from typing import Optional

log = logging.getLogger(__name__)

_cached_python: Optional[str] = None
_cache_checked: bool = False

# Python versions to probe, newest first
_VERSIONS_SHORT = ("314", "313", "312", "311", "310", "39", "38")
_VERSIONS_DOT = ("3.14", "3.13", "3.12", "3.11", "3.10", "3.9", "3.8")


def _has_tkinter(exe: str, timeout: float = 8.0) -> bool:
    """Return True if *exe* can import tkinter."""
    try:
        result = subprocess.run(
            [exe, "-c", "import tkinter; print('ok')"],
            capture_output=True,
            timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return result.returncode == 0 and b"ok" in result.stdout
    except (OSError, subprocess.SubprocessError, subprocess.TimeoutExpired) as exc:
        log.debug("tkinter probe failed for %s: %s", exe, exc)
        return False


def _candidate_paths() -> list[str]:
    """Build an ordered list of candidate Python executables."""
    candidates: list[str] = []

    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        # Standard python.org installer
        for ver in _VERSIONS_SHORT:
            candidates.append(
                os.path.join(local, "Programs", "Python", f"Python{ver}", "python.exe"))

        # Winget / Windows package-manager installs
        py_local = os.path.join(local, "Python")
        if os.path.isdir(py_local):
            try:
                for d in sorted(os.listdir(py_local), reverse=True):
                    p = os.path.join(py_local, d, "python.exe")
                    if os.path.isfile(p):
                        candidates.append(p)
            except OSError:
                pass

        candidates.append(os.path.join(local, "Python", "bin", "python.exe"))
        candidates.append(os.path.join(local, "Python", "python.exe"))

    # All-users installs under Program Files
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    for base_dir in (pf, pf86):
        for ver in _VERSIONS_DOT:
            candidates.append(
                os.path.join(base_dir, "Python",
                             f"Python{ver.replace('.', '')}", "python.exe"))

    # Legacy C:\PythonXX
    for ver in _VERSIONS_SHORT:
        candidates.append(f"C:\\Python{ver}\\python.exe")

    # PATH lookup
    for name in ("python3", "python"):
        found = shutil.which(name)
        if found:
            candidates.append(found)

    # Ultimate fallback: the Python running this script
    candidates.append(sys.executable)

    return candidates


def find_python(*, force_refresh: bool = False) -> Optional[str]:
    """Return a path to a system Python with tkinter, or None.

    The result is cached after the first successful (or unsuccessful) probe.
    Pass *force_refresh=True* to re-scan.
    """
    global _cached_python, _cache_checked

    if _cache_checked and not force_refresh:
        return _cached_python

    for exe in _candidate_paths():
        if "WindowsApps" in exe:
            continue
        if not os.path.isfile(exe):
            continue
        if _has_tkinter(exe):
            log.info("Python discovery: using %s", exe)
            _cached_python = exe
            _cache_checked = True
            return exe

    log.warning("Python discovery: no suitable Python with tkinter found")
    _cached_python = None
    _cache_checked = True
    return None
