"""Training engine (CPU PyTorch): detect and install.

The toolbox ships without torch. install_torch() runs the RUNNING
interpreter's pip with ``--target dev_root()/pyenv`` against the official
CPU wheel index, so nothing is written into the toolbox's own
site-packages and an update of the toolbox does not lose it.

The embedded CPython the toolbox bundles may not include pip, and its
``._pth`` file makes it ignore PYTHONPATH. So:
  * pip is probed first and a clear TorchInstallError is raised if absent;
  * the trainer subprocess gets its sys.path from argv (see train.py),
    never from the environment.

install_torch() is never exercised by the tests (network).
"""
from __future__ import annotations

import importlib
import importlib.metadata as md
import importlib.util
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

from . import paths

log = logging.getLogger(__name__)

Progress = Optional[Callable[[float, str], None]]

CPU_INDEX = "https://download.pytorch.org/whl/cpu"
PYPI_INDEX = "https://pypi.org/simple"


class TorchInstallError(RuntimeError):
    pass


def python_exe() -> str:
    """Console interpreter for subprocesses (pythonw has no usable stdio
    for pip progress; prefer its python.exe sibling)."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe":
        sib = exe.with_name("python.exe")
        if sib.is_file():
            return str(sib)
    return str(exe)


def _popen_flags() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def _pyenv_torch() -> Optional[tuple[str, str]]:
    pe = paths.pyenv_dir()
    if not pe.is_dir():
        return None
    for dist in md.distributions(path=[str(pe)]):
        if (dist.metadata["Name"] or "").lower() == "torch":
            return dist.version, str(pe)
    return None


def _base_torch() -> Optional[tuple[str, str]]:
    spec = importlib.util.find_spec("torch")
    if spec is None or not spec.origin:
        return None
    try:
        ver = md.version("torch")
    except md.PackageNotFoundError:
        ver = None
    return ver, str(Path(spec.origin).parent.parent)


def torch_status() -> dict:
    found = _pyenv_torch() or _base_torch()
    if not found:
        return {"installed": False, "version": None, "location": None}
    return {"installed": True, "version": found[0], "location": found[1]}


def ensure_pyenv_on_path() -> None:
    pe = str(paths.pyenv_dir())
    if paths.pyenv_dir().is_dir() and pe not in sys.path:
        sys.path.append(pe)
        importlib.invalidate_caches()


def _pip_available() -> bool:
    try:
        r = subprocess.run([python_exe(), "-m", "pip", "--version"], capture_output=True,
                           text=True, timeout=120, creationflags=_popen_flags())
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("devmode: pip probe failed: %s", exc)
        return False
    return r.returncode == 0


_PHASES = (
    (re.compile(r"^Looking in indexes"), 0.02, "contacting package index"),
    (re.compile(r"^Collecting torch"), 0.05, "resolving torch"),
    (re.compile(r"^Downloading .*torch-"), 0.10, "downloading torch (~200 MB)"),
    (re.compile(r"^Collecting "), None, None),
    (re.compile(r"^Downloading "), None, None),
    (re.compile(r"^Installing collected packages"), 0.80, "unpacking"),
    (re.compile(r"^Successfully installed"), 0.97, "installed"),
)


def install_torch(progress: Progress = None) -> None:
    def say(f: float, m: str) -> None:
        if progress:
            progress(f, m)

    say(0.0, "checking pip")
    if not _pip_available():
        raise TorchInstallError(
            "This Python has no pip, so the training engine cannot be installed "
            f"automatically ({python_exe()}). Install pip for this interpreter, or "
            f"install the CPU build of PyTorch manually into {paths.pyenv_dir()} with: "
            f"python -m pip install --target \"{paths.pyenv_dir()}\" --index-url {CPU_INDEX} torch"
        )
    target = paths.pyenv_dir()
    target.mkdir(parents=True, exist_ok=True)
    cmd = [python_exe(), "-m", "pip", "install", "--target", str(target), "--upgrade",
           "--index-url", CPU_INDEX, "--extra-index-url", PYPI_INDEX,
           "--disable-pip-version-check", "--no-warn-script-location",
           "--progress-bar", "off", "torch"]
    log.info("devmode: %s", " ".join(cmd))
    log_path = paths.sub("logs") / "install_torch.log"
    frac = 0.0
    tail: list[str] = []
    with open(log_path, "w", encoding="utf-8") as lf:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                encoding="utf-8", errors="replace", creationflags=_popen_flags())
        assert proc.stdout is not None
        for line in proc.stdout:
            lf.write(line)
            s = line.strip()
            tail = (tail + [s])[-15:]
            for rx, f, msg in _PHASES:
                if rx.search(s):
                    if f is None:
                        frac = min(0.78, frac + 0.02)
                        say(frac, s[:80])
                    else:
                        frac = max(frac, f)
                        say(frac, msg)
                    break
        rc = proc.wait()
    if rc != 0:
        raise TorchInstallError(f"pip exited {rc}; see {log_path}\n" + "\n".join(tail))
    ensure_pyenv_on_path()
    st = torch_status()
    if not st["installed"]:
        raise TorchInstallError(f"pip reported success but torch is not in {target}")
    say(1.0, f"torch {st['version']} ready")
