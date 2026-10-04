# Everything Finder -- agent "everything-finder" (claude-opus-5-5 subagent; no runtime agent id exposed)
# written 2026-10-03T21:47-0400, parent: session:7bee459a
"""Put an existing tool window's contents inside the Everything Finder.

Item Finder and Trade Hub are both built as top-level ``SCWindow``
(``QMainWindow``) subclasses. Rather than rewrite them, the Everything Finder
constructs the tool window exactly as its own launcher does and then
*transplants its central widget* into a tab:

* ``takeCentralWidget()`` hands over the whole holo surface - title bar,
  toolbars, tables - with every signal connection intact. The tool's own
  Python object keeps running its timers, workers and slots.
* The emptied tool window stays alive as a HIDDEN child window of the
  Everything Finder (so it is destroyed with it, and is never shown).
* Its geometry is kept equal to the Everything Finder's, so anything the tool
  positions relative to ``self`` (pop-out bubbles, dialogs) lands on screen
  where the user is looking.
* Its ``show`` / ``raise_`` are redirected to the Everything Finder, so a code
  path that tries to bring the tool forward brings the umbrella forward instead
  of popping an empty frame.
* The tool's title bar stays (it carries the tool's own status label and
  buttons), but its window chrome - close, minimise, collapse, fullscreen,
  reset and the opacity slider - is hidden, and it is retargeted at the
  Everything Finder so dragging it moves the whole window.
* With ``hide_title=True`` the title bar's own icon and name are hidden too.
  The tab already names the tool, and Item Finder's bar still reads "MARKET
  FINDER" (its old name), which under an "ITEM FINDER" tab looks like a
  different tool. Its status label and buttons sit after the bar's stretch
  and stay.

Why transplant rather than ``setWindowFlags(Qt.Widget)`` on the QMainWindow:
the app-wide edge-resize filter (shared/qt/base_window.py) walks a widget's
parents to the FIRST ``SCWindow`` it meets. An embedded SCWindow would be that
first ancestor for everything in the tab, and the filter would then refuse to
resize the real window from any edge over the tool. With the surface
transplanted, the first SCWindow up the chain is the Everything Finder.
"""
from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtWidgets import QLabel, QMainWindow, QWidget

from shared.qt.title_bar import SCTitleBar, _TitleButton


def strip_title_chrome(title_bar: SCTitleBar, outer: QWidget) -> None:
    """Hide a tool title bar's window controls and point it at *outer*."""
    title_bar._window = outer                 # drag / double-click act on the umbrella
    for btn in title_bar.findChildren(_TitleButton):
        btn.hide()
    slider = getattr(title_bar, "_opacity_slider", None)
    if slider is not None:
        slider.hide()
    for lbl in title_bar.findChildren(QLabel):
        if lbl.text() == "◉":            # the opacity slider's icon
            lbl.hide()


def hide_title_text(title_bar: SCTitleBar) -> int:
    """Hide a tool title bar's icon and name; returns how many labels it hid.

    SCTitleBar lays out [icon] [title] [hotkey badge] <stretch> ...controls.
    Everything a tool adds for itself (status line, buttons) goes after the
    stretch, so "the QLabels before the first stretch" is exactly the header.
    """
    lay = title_bar.layout()
    hidden = 0
    for i in range(lay.count() if lay is not None else 0):
        item = lay.itemAt(i)
        if item.spacerItem() is not None:
            break
        w = item.widget()
        if isinstance(w, QLabel):
            w.hide()
            hidden += 1
    return hidden


def embed_window(inner: QMainWindow, outer: QMainWindow,
                 on_reveal: Optional[Callable[[], None]] = None,
                 hide_title: bool = False) -> QWidget:
    """Transplant *inner*'s central widget for use inside *outer*.

    Returns the widget to put in a tab. *on_reveal* runs when the tool asks to
    be shown (defaults to showing and raising *outer*). *hide_title* also hides
    the tool's own icon + name (see :func:`hide_title_text`)."""
    central = inner.takeCentralWidget()
    if central is None:
        raise RuntimeError(f"{type(inner).__name__} has no central widget to embed")
    inner.hide()
    # Owned by the umbrella (destroyed with it) but still its own, never-shown window.
    inner.setParent(outer, inner.windowFlags())
    inner.hide()
    inner.setGeometry(outer.geometry())

    def _reveal(*_a, **_k) -> None:
        if on_reveal is not None:
            on_reveal()
        else:
            outer.show()
            outer.raise_()
    inner.show = _reveal          # Python-level callers only, which is all the tools use
    inner.raise_ = _reveal

    for tb in central.findChildren(SCTitleBar):
        if getattr(tb, "_window", None) is inner:
            strip_title_chrome(tb, outer)
            if hide_title:
                hide_title_text(tb)
    return central
