import os
import sys

# Bootstrap SuitMk2's own package (and its core/, which the ui imports flat), plus the
# toolbox root for `shared`, onto sys.path.
_SUIT_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
_TOOLBOX = os.path.normpath(os.path.join(_SUIT_ROOT, "..", ".."))
# Order matters: the toolbox root has a `ui/` package of its own, so SuitMk2's must come
# first on sys.path or `import ui.suit_window` resolves to the wrong one.
for _p in (_TOOLBOX, _SUIT_ROOT, os.path.join(_SUIT_ROOT, "core")):
    if _p in sys.path:
        sys.path.remove(_p)
    sys.path.insert(0, _p)
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
