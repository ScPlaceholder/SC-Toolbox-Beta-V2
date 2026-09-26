"""Shared scaffolding for the seven step pages."""

from __future__ import annotations

from typing import Any, Callable, Iterable, Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QLabel, QProgressBar, QSizePolicy, QWidget

from shared.qt.theme import P

from ..backend import BackendHandle
from ..style import MONO, PROGRESS_QSS
from ..workers import TaskRunner


class StepPage(QWidget):
    """One screen of the step rail.

    Subclasses build their widgets in ``__init__`` and fetch data in
    ``refresh()`` (called every time the page is shown). ``summary`` is the
    one-line status shown under the page's name in the rail.
    """

    title = ""
    summary_changed = Signal(str)

    def __init__(self, handle: BackendHandle, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.handle = handle
        self.api = handle.api
        self.runner = TaskRunner(self)
        self._summary = ""

    def refresh(self) -> None:           # pragma: no cover - overridden
        pass

    def set_summary(self, text: str) -> None:
        if text != self._summary:
            self._summary = text
            self.summary_changed.emit(text)

    @property
    def summary(self) -> str:
        return self._summary

    # A long call with a progress bar and a status label, disabling the
    # given widgets while it runs and restoring them afterwards.
    def run_long(self, name: str, fn: Callable, *args,
                 bar: Optional[QProgressBar] = None,
                 status: Optional[QLabel] = None,
                 disable: Iterable[QWidget] = (),
                 on_done: Optional[Callable[[Any], None]] = None,
                 on_error: Optional[Callable[[str], None]] = None) -> int:
        widgets = [w for w in disable if w is not None]
        prior = [w.isEnabled() for w in widgets]
        for w in widgets:
            w.setEnabled(False)
        if bar is not None:
            bar.setValue(0)
            bar.setVisible(True)
        if status is not None:
            status.setText(f"{name}…")
            status.setStyleSheet(status_style(P.fg))

        def restore():
            for w, was in zip(widgets, prior):
                w.setEnabled(was)

        def progress(frac: float, msg: str):
            if bar is not None:
                bar.setValue(int(frac * 1000))
                bar.setFormat(f"{frac * 100:.0f}%")
            if status is not None and msg:
                status.setText(msg)

        def done(result):
            restore()
            if bar is not None:
                bar.setValue(1000)
                bar.setFormat("done")
            if on_done:
                on_done(result)

        def failed(message: str):
            restore()
            if bar is not None:
                bar.setFormat("failed")
            if status is not None:
                status.setText(f"{name} failed: {message}")
                status.setStyleSheet(status_style(P.red))
            if on_error:
                on_error(message)

        return self.runner.start(name, fn, *args, on_done=done, on_error=failed,
                                 on_progress=progress)


def status_style(colour: str) -> str:
    return f"color: {colour}; font-family: {MONO}; font-size: 9pt; background: transparent;"


def progress_bar(parent: Optional[QWidget] = None, visible: bool = False) -> QProgressBar:
    bar = QProgressBar(parent)
    bar.setRange(0, 1000)
    bar.setValue(0)
    bar.setTextVisible(True)
    bar.setFormat("")
    bar.setStyleSheet(PROGRESS_QSS)
    bar.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
    bar.setVisible(visible)
    return bar


class StatusLabel(QLabel):
    """A status line that takes no space while it has nothing to say."""

    def setText(self, text: str) -> None:  # noqa: N802 - Qt naming
        super().setText(text)
        self.setVisible(bool(text))


def status_label(parent: Optional[QWidget] = None, text: str = "") -> QLabel:
    lbl = StatusLabel(parent)
    lbl.setText(text)
    lbl.setWordWrap(True)
    lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
    lbl.setStyleSheet(status_style(P.fg_dim))
    return lbl


def load_qimage(path: Optional[str]) -> Optional[QImage]:
    """Load an image off the GUI thread (QImage is thread-safe, QPixmap isn't)."""
    if not path:
        return None
    img = QImage(str(path))
    return None if img.isNull() else img


def scaled_pixmap(img: Optional[QImage], max_w: int, max_h: int,
                  integer: bool = True) -> QPixmap:
    """Scale up crisply: whole-number factors with nearest-neighbour so every
    source pixel stays visible (that is what a labeller needs to see)."""
    if img is None or img.isNull() or max_w <= 0 or max_h <= 0:
        return QPixmap()
    fx, fy = max_w / img.width(), max_h / img.height()
    f = min(fx, fy)
    if integer and f >= 1:
        f = max(1, int(f))
    w, h = max(1, int(img.width() * f)), max(1, int(img.height() * f))
    mode = Qt.FastTransformation if f >= 1 else Qt.SmoothTransformation
    return QPixmap.fromImage(img.scaled(w, h, Qt.IgnoreAspectRatio, mode))
