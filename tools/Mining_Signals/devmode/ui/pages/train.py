"""Step 6 — train a candidate per region kind, benchmark it against the
model in use on held-out confirmed crops, activate it only if it wins."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QFrame, QGridLayout, QHeaderView, QLabel, QProgressBar,
    QPushButton, QScrollArea, QStackedWidget, QTableWidget, QTableWidgetItem, QWidget,
    QVBoxLayout,
)

from shared.qt.theme import P

from ..style import (
    ACCENT, HEAD, KIND_LABELS, KINDS, MONO, TABLE_QSS, Banner, Card, button, fmt_pct,
    page_frame, subtext,
)
from .base import StepPage, progress_bar, status_style

# Fewer held-out crops than this and a benchmark difference is mostly noise.
MIN_TRUSTED_N = 30
NO_TORCH = "Install the training engine first (step 1, Engine)."


@dataclass
class _Row:
    name: QPushButton
    labels: QLabel
    train: QPushButton
    progress_stack: QStackedWidget
    idle: QLabel
    bar: QProgressBar
    current: QLabel
    candidate: QLabel
    activate: QPushButton
    revert: QPushButton
    reason: QLabel


class TrainPage(StepPage):
    title = "Train & Test"
    go_to_step = Signal(int)

    def __init__(self, handle, parent: Optional[QWidget] = None):
        super().__init__(handle, parent)
        self._torch = False
        self._cmp: dict[str, dict] = {}
        self._confirmed: dict[str, int] = {}
        self._active: dict[str, Optional[str]] = {}
        self._selected = "signal_rgb"
        _, root = page_frame(self, "Train & test",
                             "Train a candidate model per region kind, then score it and the model "
                             "you use now on the same held-out crops: confirmed labels that training "
                             "never saw. Activate stays locked unless the candidate scores higher.")

        self.banner = Banner(self, "warn")
        self.banner.set_text(f"<b>Training is locked.</b> {NO_TORCH} Benchmarking still works.")
        self.banner.add_action("Go to Engine", lambda: self.go_to_step.emit(0))
        root.addWidget(self.banner)

        card = Card(self, margins=(12, 10, 12, 10))
        grid = QGridLayout()
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(3)
        for c, h in enumerate(("REGION KIND", "", "PROGRESS", "IN USE NOW", "NEW CANDIDATE", "", "")):
            lbl = QLabel(h, card)
            lbl.setStyleSheet(f"color: {ACCENT}; font-family: {HEAD}; font-size: 8pt; "
                              f"font-weight: bold; background: transparent;")
            grid.addWidget(lbl, 0, c)
        self.rows: dict[str, _Row] = {}
        r = 1
        for kind in KINDS:
            self.rows[kind] = self._build_row(card, grid, r, kind)
            r += 4
        grid.setColumnStretch(2, 1)
        grid.setColumnMinimumWidth(2, 150)
        card.body.addLayout(grid)
        root.addWidget(card)

        detail = Card(self, "")
        self.detail_title = QLabel("", detail)
        self.detail_title.setStyleSheet(f"color: {ACCENT}; font-family: {HEAD}; font-size: 9pt; "
                                        f"font-weight: bold; background: transparent;")
        detail.body.addWidget(self.detail_title)
        self.detail = QTableWidget(0, 1, detail)
        self.detail.setStyleSheet(TABLE_QSS)
        self.detail.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.detail.setSelectionMode(QAbstractItemView.NoSelection)
        self.detail.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.detail.verticalHeader().setDefaultSectionSize(24)
        self.detail.setMinimumHeight(110)
        detail.body.addWidget(self.detail, 1)
        root.addWidget(detail, 1)
        self.set_torch({"installed": False})

    def _build_row(self, parent, grid: QGridLayout, r: int, kind: str) -> _Row:
        name = QPushButton(KIND_LABELS[kind], parent)
        name.setCursor(Qt.PointingHandCursor)
        name.setToolTip("Show per-character scores below")
        name.setStyleSheet(
            f"QPushButton {{ background: transparent; border: none; color: {P.fg_bright}; "
            f"font-family: {MONO}; font-size: 10pt; font-weight: bold; text-align: left; "
            f"padding: 0px; min-height: 0px; }}"
            f"QPushButton:hover {{ color: {ACCENT}; }}")
        name.clicked.connect(lambda _c=False, k=kind: self.select(k))
        labels = QLabel("", parent)
        labels.setStyleSheet(status_style(P.fg_dim))
        train = button("Train", parent, "primary")
        train.clicked.connect(lambda _c=False, k=kind: self.train(k))
        stack = QStackedWidget(parent)
        stack.setFixedHeight(22)
        idle = QLabel("", stack)
        idle.setStyleSheet(status_style(P.fg_dim))
        bar = progress_bar(stack, visible=True)
        stack.addWidget(idle)
        stack.addWidget(bar)
        cur = QLabel("—", parent)
        cand = QLabel("—", parent)
        for lbl in (cur, cand):
            lbl.setTextFormat(Qt.RichText)
            lbl.setStyleSheet(f"color: {P.fg}; font-family: {MONO}; font-size: 10pt; "
                              f"background: transparent;")
        act = button("Activate", parent, "primary")
        act.clicked.connect(lambda _c=False, k=kind: self.activate(k))
        rev = button("Revert to stock", parent)
        rev.clicked.connect(lambda _c=False, k=kind: self.revert(k))
        reason = QLabel("", parent)
        reason.setWordWrap(True)
        reason.setStyleSheet(status_style(P.fg_dim))

        grid.addWidget(name, r, 0)
        grid.addWidget(train, r, 1, 2, 1, Qt.AlignVCenter)
        grid.addWidget(stack, r, 2, 2, 1, Qt.AlignVCenter)
        grid.addWidget(cur, r, 3, 2, 1, Qt.AlignVCenter)
        grid.addWidget(cand, r, 4, 2, 1, Qt.AlignVCenter)
        grid.addWidget(act, r, 5, 2, 1, Qt.AlignVCenter)
        grid.addWidget(rev, r, 6, 2, 1, Qt.AlignVCenter)
        grid.addWidget(labels, r + 1, 0)
        grid.addWidget(reason, r + 2, 0, 1, 7)
        sep = QFrame(parent)
        sep.setFixedHeight(1)
        sep.setStyleSheet(f"background: {P.border};")
        grid.addWidget(sep, r + 3, 0, 1, 7)
        return _Row(name, labels, train, stack, idle, bar, cur, cand, act, rev, reason)

    # ── engine gate ──
    def set_torch(self, status: dict) -> None:
        self._torch = bool((status or {}).get("installed"))
        self.banner.setVisible(not self._torch)
        for kind in KINDS:
            self._update_row(kind)

    # ── data ──
    def refresh(self) -> None:
        self.runner.start("Checking training engine", self.api.torch_status,
                          on_done=self.set_torch)
        for kind in KINDS:
            self._bench(kind)

    def _bench(self, kind: str) -> None:
        row = self.rows[kind]
        row.current.setText(f"<span style='color:{P.fg_dim}'>scoring…</span>")
        self.runner.start(f"Benchmarking {kind}", self._fetch, kind,
                          on_done=lambda d, k=kind: self._got(k, d),
                          on_error=lambda m, k=kind: self._bench_failed(k, m))

    def _fetch(self, kind: str) -> dict:
        return {"cmp": self.api.compare(kind),
                "confirmed": int(self.api.label_stats(kind).get("confirmed", 0)),
                "active": self.api.active_model_path(kind)}

    def _got(self, kind: str, d: dict) -> None:
        self._cmp[kind] = d["cmp"] or {}
        self._confirmed[kind] = d["confirmed"]
        self._active[kind] = d["active"]
        self._update_row(kind)
        if kind == self._selected:
            self._fill_detail()
        cands = sum(1 for c in self._cmp.values() if c.get("candidate"))
        better = sum(1 for c in self._cmp.values() if c.get("better"))
        self.set_summary(f"{better} ready to activate" if better else
                         (f"{cands} candidate(s)" if cands else "nothing trained"))

    def _bench_failed(self, kind: str, msg: str) -> None:
        row = self.rows[kind]
        row.current.setText(f"<span style='color:{P.red}'>error</span>")
        row.reason.setText(f"Benchmark failed: {msg}")
        row.reason.setStyleSheet(status_style(P.red))

    def _update_row(self, kind: str) -> None:
        row = self.rows[kind]
        cmp = self._cmp.get(kind, {})
        cur, cand = cmp.get("current"), cmp.get("candidate")
        better = bool(cmp.get("better"))
        training = self.runner.is_running(f"Training {kind}")
        active = self._active.get(kind)
        n_conf = self._confirmed.get(kind)

        row.labels.setText("" if n_conf is None else
                           f"{n_conf:,} confirmed · " + ("your model" if active else "stock model"))
        row.train.setEnabled(self._torch and not training)
        row.train.setToolTip("" if self._torch else NO_TORCH)
        if not training:
            row.progress_stack.setCurrentIndex(0)
            row.idle.setText("training locked" if not self._torch else
                             ("candidate ready" if cand else "not trained yet"))
        if cur:
            row.current.setText(self._acc_html(cur))
        if cand:
            delta = (cand.get("accuracy", 0) - (cur or {}).get("accuracy", 0)) * 100
            colour = P.green if better else P.red
            row.candidate.setText(f"{self._acc_html(cand)} <b style='color:{colour}'>"
                                  f"{delta:+.1f}</b>")
        else:
            row.candidate.setText(f"<span style='color:{P.fg_dim}'>none</span>")
        row.activate.setEnabled(better and not training)
        row.revert.setEnabled(bool(active) and not training)
        row.revert.setToolTip("Go back to the model that shipped with the toolbox."
                              if active else "Already using the stock model.")

        # The reason line says why Activate is (not) available, in words.
        colour = P.fg_dim
        if training:
            text = "Training… you can keep labelling in the meantime."
        elif not cmp:
            text = ""
        elif not cand:
            text = ("No candidate yet: train one to compare." if self._torch else
                    "No candidate yet. " + NO_TORCH)
            row.activate.setToolTip(text)
        elif better:
            text = (f"Candidate beats the model in use by {(cand['accuracy'] - cur['accuracy']) * 100:.1f} "
                    f"points on {cur.get('n', 0):,} held-out crops. Activate to start using it.")
            colour = P.green
            row.activate.setToolTip("Use the candidate from now on (Revert undoes it).")
        else:
            text = ("Candidate is not better than the model in use, so it can't be activated. "
                    "Label more crops (especially corrections) and train again.")
            colour = P.yellow
            row.activate.setToolTip(text)
        n = (cur or {}).get("n")
        if cmp and n == 0:
            text += ("  No held-out crops yet, so there is nothing to score against: confirm "
                     "some captures of this kind in step 3.")
            colour = P.yellow
        elif cmp and n is not None and n < MIN_TRUSTED_N:
            text += (f"  Only {n} held-out crops: differences this small are mostly noise until "
                     f"you have labelled more.")
            colour = P.yellow
        row.reason.setText(text)
        row.reason.setStyleSheet(status_style(colour))
        row.reason.setVisible(bool(text))

    @staticmethod
    def _acc_html(b: dict) -> str:
        if not b.get("n"):
            return f"<span style='color:{P.fg_dim}'>no test crops</span>"
        return (f"<b style='color:{P.fg_bright}'>{fmt_pct(b.get('accuracy'))}</b>"
                f" <span style='color:{P.fg_dim}'>n={b.get('n', 0)}</span>")

    # ── detail ──
    def select(self, kind: str) -> None:
        self._selected = kind
        self._fill_detail()

    def _fill_detail(self) -> None:
        kind = self._selected
        cmp = self._cmp.get(kind, {})
        cur, cand = cmp.get("current") or {}, cmp.get("candidate") or {}
        self.detail_title.setText(f"PER-CHARACTER ACCURACY  ·  {KIND_LABELS[kind].upper()}"
                                  "   (click a kind above to switch)")
        chars = sorted(set(cur.get("per_class", {})) | set(cand.get("per_class", {})))
        self.detail.clear()
        self.detail.setRowCount(2)
        self.detail.setColumnCount(len(chars))
        self.detail.setHorizontalHeaderLabels(chars)
        self.detail.setVerticalHeaderLabels(["In use now", "Candidate"])
        self.detail.verticalHeader().setVisible(True)
        self.detail.verticalHeader().setStyleSheet(
            f"QHeaderView::section {{ background: {P.bg_header}; color: {P.fg}; border: none; "
            f"padding: 2px 8px; font-family: {MONO}; font-size: 8pt; }}")
        for c, ch in enumerate(chars):
            a = cur.get("per_class", {}).get(ch)
            b = cand.get("per_class", {}).get(ch)
            for r, v in enumerate((a, b)):
                it = QTableWidgetItem("—" if v is None else f"{v * 100:.0f}%")
                it.setTextAlignment(Qt.AlignCenter)
                if r == 1 and a is not None and v is not None:
                    it.setForeground(QColor(P.green if v > a else (P.red if v < a else P.fg)))
                self.detail.setItem(r, c, it)

    # ── actions ──
    def train(self, kind: str) -> None:
        if not self._torch:
            self.rows[kind].reason.setText(NO_TORCH)
            return
        row = self.rows[kind]
        row.progress_stack.setCurrentIndex(1)
        row.bar.setValue(0)
        row.bar.setFormat("starting")

        def prog(frac: float, msg: str):
            row.bar.setValue(int(frac * 1000))
            row.bar.setFormat(f"{frac * 100:.0f}%  {msg}"[:40])

        self.runner.start(f"Training {kind}", self.api.train, kind, on_progress=prog,
                          on_done=lambda _p, k=kind: self._trained(k),
                          on_error=lambda m, k=kind: self._train_failed(k, m))
        self._update_row(kind)

    def _trained(self, kind: str) -> None:
        self._update_row(kind)
        self._bench(kind)

    def _train_failed(self, kind: str, msg: str) -> None:
        self._update_row(kind)
        row = self.rows[kind]
        row.reason.setVisible(True)
        row.reason.setText(f"Training failed: {msg}")
        row.reason.setStyleSheet(status_style(P.red))

    def activate(self, kind: str) -> None:
        if not self._cmp.get(kind, {}).get("better"):
            return
        self.rows[kind].activate.setEnabled(False)
        self.runner.start(f"Activating {kind}", self.api.activate, kind,
                          on_done=lambda ok, k=kind: self._activated(k, ok))

    def _activated(self, kind: str, ok: bool) -> None:
        self._bench(kind)
        row = self.rows[kind]
        if not ok:
            row.reason.setText("Not activated: on a fresh check the candidate did not beat the "
                               "model in use.")
            row.reason.setStyleSheet(status_style(P.yellow))

    def revert(self, kind: str) -> None:
        self.rows[kind].revert.setEnabled(False)
        self.runner.start(f"Reverting {kind}", self.api.revert, kind,
                          on_done=lambda _r, k=kind: self._bench(k))
