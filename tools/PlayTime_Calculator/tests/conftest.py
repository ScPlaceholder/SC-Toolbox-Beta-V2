import os
import sys

# Bootstrap the PlayTime tool root (core/, ui/) + repo root (shared/) onto sys.path
_PT_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
if _PT_ROOT not in sys.path:
    sys.path.insert(0, _PT_ROOT)
_REPO_ROOT = os.path.normpath(os.path.join(_PT_ROOT, "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.append(_REPO_ROOT)
