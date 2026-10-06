"""A stacked widget whose pages are built the first time they are shown.

Each tab is registered with a *factory* (a zero-argument callable returning a
QWidget). Until a tab is selected its page is a cheap placeholder label; the
factory runs exactly once, on first selection, and its widget replaces the
placeholder. Re-selecting a built tab only switches pages.

This is what keeps the Everything Finder's open cost at roughly one tool: the
window builds the tab it opens on and nothing else.

A factory that raises does not take the umbrella window down: the page shows
the error instead, and the tab stays unbuilt so selecting it again retries.
"""
from __future__ import annotations

import logging
from typing import Callable, Dict, List, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QLabel, QStackedWidget, QWidget

from shared.qt.theme import P

log = logging.getLogger(__name__)

Factory = Callable[[], QWidget]


class LazyTabStack(QStackedWidget):
    """QStackedWidget with build-on-first-select pages, addressed by key."""

    tabBuilt = Signal(str)            # key, once, after its factory returned a widget
    currentKeyChanged = Signal(str)   # key, every time the visible tab changes

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self._order: List[str] = []
        self._labels: Dict[str, str] = {}
        self._factories: Dict[str, Factory] = {}
        self._pages: Dict[str, QWidget] = {}        # placeholder until built, then the real widget
        self._built: Dict[str, QWidget] = {}
        self._current: str = ""
        self._building: str = ""

    # registration
    def add_tab(self, key: str, label: str, factory: Factory) -> None:
        if key in self._factories:
            raise ValueError(f"tab {key!r} already registered")
        self._order.append(key)
        self._labels[key] = label
        self._factories[key] = factory
        ph = self._placeholder(f"{label}\n\nopens when you select this tab")
        self._pages[key] = ph
        self.addWidget(ph)

    # queries
    def keys(self) -> List[str]:
        return list(self._order)

    def label(self, key: str) -> str:
        return self._labels.get(key, key)

    def is_built(self, key: str) -> bool:
        return key in self._built

    def built_keys(self) -> List[str]:
        return [k for k in self._order if k in self._built]

    def built_widget(self, key: str) -> Optional[QWidget]:
        return self._built.get(key)

    def current_key(self) -> str:
        return self._current

    # selection
    def select(self, key: str) -> Optional[QWidget]:
        """Show *key*, building it first if this is its first selection.

        Returns the tab's real widget, or None when its factory failed."""
        if key not in self._factories:
            raise KeyError(key)
        if key not in self._built and self._building != key:
            self._build(key)
        page = self._pages[key]
        self.setCurrentWidget(page)
        if key != self._current:
            self._current = key
            self.currentKeyChanged.emit(key)
        return self._built.get(key)

    def cycle(self, step: int = 1) -> str:
        """Select the next (or previous) tab, wrapping. Returns the new key."""
        if not self._order:
            return ""
        i = self._order.index(self._current) if self._current in self._order else -1
        key = self._order[(i + step) % len(self._order)]
        self.select(key)
        return key

    # internals
    def _build(self, key: str) -> None:
        self._building = key
        try:
            widget = self._factories[key]()
        except Exception as exc:     # an embedded tool failing must not kill the umbrella
            log.exception("Everything Finder: building the %s tab failed", key)
            self._swap_page(key, self._placeholder(
                f"{self._labels[key]} could not open:\n{type(exc).__name__}: {exc}\n\n"
                "Select the tab again to retry."))
            return
        finally:
            self._building = ""
        if widget is None:
            self._swap_page(key, self._placeholder(f"{self._labels[key]} returned nothing."))
            return
        self._built[key] = widget
        self._swap_page(key, widget)
        self.tabBuilt.emit(key)

    def _swap_page(self, key: str, widget: QWidget) -> None:
        old = self._pages.get(key)
        idx = self.indexOf(old) if old is not None else -1
        if idx >= 0:
            # Only ever called on an UNBUILT tab, so *old* is always a placeholder.
            self.insertWidget(idx, widget)
            self.removeWidget(old)
            if old is not widget:
                old.deleteLater()
        else:
            self.addWidget(widget)
        self._pages[key] = widget

    @staticmethod
    def _placeholder(text: str) -> QWidget:
        lbl = QLabel(text)
        lbl.setAlignment(Qt.AlignCenter)
        lbl.setWordWrap(True)
        lbl.setStyleSheet(
            f"color: {P.fg_dim}; font-family: Consolas; font-size: 10pt; "
            f"background: transparent; padding: 24px;")
        return lbl
