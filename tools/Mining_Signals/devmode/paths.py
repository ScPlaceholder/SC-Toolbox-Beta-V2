"""Filesystem layout for Dev Mode.

User data lives OUTSIDE the install folder (the Velopack updater wipes the
install on every update):

    DEV_ROOT = ~/.sctoolbox/mining_signals/devmode/     (env SC_DEVMODE_ROOT overrides)

    DEV_ROOT/devmode.sqlite3        capture + glyph index
    DEV_ROOT/state.json             capture toggle, size cap, game info
    DEV_ROOT/captures/<family>/     capture crops (PNG)
    DEV_ROOT/glyphs/<pool>/<cls>/   extracted 28x28 glyphs
    DEV_ROOT/synth/<pool>/<cls>/    synthetic glyphs
    DEV_ROOT/models/<kind>/         candidate.onnx / active.onnx (+ .json)
    DEV_ROOT/pyenv/                 CPU PyTorch installed with pip --target
    DEV_ROOT/logs/                  trainer + extractor logs

The environment variable is re-read on every call so tests can point each
case at its own tmp_path.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

DEVMODE_DIR = Path(__file__).resolve().parent
TOOL_DIR = DEVMODE_DIR.parent                  # tools/Mining_Signals
OCR_DIR = TOOL_DIR / "ocr"
SCRIPTS_DIR = TOOL_DIR / "scripts"
REPO_ROOT = TOOL_DIR.parent.parent             # SC_Toolbox_Beta_V1.2

ENV_ROOT = "SC_DEVMODE_ROOT"


def dev_root() -> Path:
    env = os.environ.get(ENV_ROOT)
    root = Path(env) if env else Path.home() / ".sctoolbox" / "mining_signals" / "devmode"
    root.mkdir(parents=True, exist_ok=True)
    return root


def sub(*parts: str) -> Path:
    """A directory under dev_root(), created on demand."""
    p = dev_root().joinpath(*parts)
    p.mkdir(parents=True, exist_ok=True)
    return p


def pyenv_dir() -> Path:
    return dev_root() / "pyenv"


def ensure_import_paths() -> None:
    """Make ``ocr`` (tools/Mining_Signals) and the ``scripts`` helpers
    importable. Appended, never prepended, so nothing here can shadow a
    module the host application already resolved."""
    for p in (str(TOOL_DIR), str(SCRIPTS_DIR)):
        if p not in sys.path:
            sys.path.append(p)


def is_inside_install(path: Path) -> bool:
    """True when ``path`` is inside the install tree (where writes are forbidden)."""
    try:
        Path(path).resolve().relative_to(TOOL_DIR.resolve())
    except ValueError:
        return False
    return True
