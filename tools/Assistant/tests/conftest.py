import os
import sys

# Bootstrap the Assistant's own package, and the toolbox root for `shared`, onto sys.path.
_A_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
_TOOLBOX = os.path.normpath(os.path.join(_A_ROOT, "..", ".."))
for _p in (_A_ROOT, _TOOLBOX):
    if _p not in sys.path:
        sys.path.insert(0, _p)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
