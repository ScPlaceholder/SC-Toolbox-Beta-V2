"""Step 1 — the training engine (PyTorch, CPU build)."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QHBoxLayout, QLabel, QWidget

from shared.qt.theme import P

from ..style import ACCENT, HEAD, MONO, Card, button, page_frame, subtext
from .base import StepPage, progress_bar, status_label


def _state_style(colour: str) -> str:
    return (f"font-family: {HEAD}; font-size: 18pt; font-weight: bold; color: {colour}; "
            f"background: transparent; letter-spacing: 2px;")


class EnginePage(StepPage):
    title = "Engine"
    torch_changed = Signal(dict)

    def __init__(self, handle, parent: Optional[QWidget] = None):
        super().__init__(handle, parent)
        self._torch: dict = {"installed": False, "version": None, "location": None}
        _, root = page_frame(self, "Training engine",
                             "Labelling, glyph review, synthetic data and export all work "
                             "without this. Only step 6 (training a new model) needs it.")

        card = Card(self, "PyTorch (CPU)")
        self.state = QLabel("CHECKING…", card)
        self.state.setStyleSheet(_state_style(P.fg_dim))
        card.body.addWidget(self.state)
        self.detail = subtext("", card)
        self.detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        card.body.addWidget(self.detail)

        row = QHBoxLayout()
        row.setSpacing(10)
        self.install_btn = button("Install training engine (~200 MB download)", card, "primary")
        self.install_btn.clicked.connect(self.install)
        row.addWidget(self.install_btn)
        self.recheck_btn = button("Check again", card)
        self.recheck_btn.clicked.connect(self.refresh)
        row.addWidget(self.recheck_btn)
        row.addStretch(1)
        card.body.addLayout(row)
        self.bar = progress_bar(card)
        card.body.addWidget(self.bar)
        self.status = status_label(card)
        card.body.addWidget(self.status)
        card.body.addWidget(subtext(
            "The installer downloads the CPU-only PyTorch wheel with pip into your Dev Mode "
            "folder (not the toolbox install), so toolbox updates don't delete it and it never "
            "touches any other Python on this PC. No GPU is needed.", card))
        root.addWidget(card)

        where = Card(self, "Where your Dev Mode data lives")
        self.root_label = QLabel("", where)
        self.root_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.root_label.setStyleSheet(
            f"font-family: {MONO}; font-size: 10pt; color: {ACCENT}; background: transparent;")
        where.body.addWidget(self.root_label)
        wrow = QHBoxLayout()
        self.open_btn = button("Open folder", where)
        self.open_btn.clicked.connect(self._open_root)
        wrow.addWidget(self.open_btn)
        wrow.addStretch(1)
        where.body.addLayout(wrow)
        where.body.addWidget(subtext(
            "Captures, your labels, approved glyphs and any model you train are kept here, "
            "outside the toolbox folder, so updating the toolbox never erases them.", where))
        root.addWidget(where)
        root.addStretch(1)

    # ── data ──
    def refresh(self) -> None:
        self.runner.start("Checking training engine", self._probe, on_done=self._show)

    def _probe(self) -> dict:
        return {"torch": self.api.torch_status(), "root": str(self.api.dev_root())}

    def _show(self, data: dict) -> None:
        self.root_label.setText(data.get("root", ""))
        self._set_torch(data.get("torch") or {})

    def _set_torch(self, st: dict) -> None:
        self._torch = dict(st)
        if st.get("installed"):
            self.state.setText(f"INSTALLED  ·  torch {st.get('version') or '?'}")
            self.state.setStyleSheet(_state_style(P.green))
            self.detail.setText(f"Location: {st.get('location') or 'on the Python path'}")
            self.install_btn.setEnabled(False)
            self.install_btn.setText("Training engine installed")
            self.set_summary("installed")
        else:
            self.state.setText("NOT INSTALLED")
            self.state.setStyleSheet(_state_style(P.yellow))
            self.detail.setText("Training (step 6) stays locked until this is installed.")
            self.install_btn.setEnabled(not self.runner.is_running("Installing training engine"))
            self.install_btn.setText("Install training engine (~200 MB download)")
            self.set_summary("not installed")
        self.torch_changed.emit(self._torch)

    @property
    def torch(self) -> dict:
        return dict(self._torch)

    # ── actions ──
    def install(self) -> None:
        self.run_long("Installing training engine", self.api.install_torch,
                      bar=self.bar, status=self.status,
                      disable=(self.install_btn, self.recheck_btn),
                      on_done=lambda _r: self._after_install())

    def _after_install(self) -> None:
        self.status.setText("Installed. Checking it imports…")
        self.refresh()

    def _open_root(self) -> None:
        path = self.root_label.text()
        if path:
            QDesktopServices.openUrl(QUrl.fromLocalFile(path))
