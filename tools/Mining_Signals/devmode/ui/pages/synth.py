"""Step 5 — top weak character classes up with synthetic samples."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QHBoxLayout, QHeaderView, QLabel, QSpinBox,
    QTableWidget, QTableWidgetItem, QWidget,
)

from shared.qt.theme import P

from ..style import (
    ACCENT, COMBO_QSS, HEAD, KIND_LABELS, KINDS, MONO, TABLE_QSS, Card, button,
    page_frame, subtext,
)
from .base import StepPage, progress_bar, status_label
from .glyphs import FONT_COLOUR, WEAK, _char_name, _src

COLS = ("Character", "Approved real", "Approved font", "Waiting review", "Will generate",
        "Seeds", "Generated")
C_REAL, C_FONT, C_WAIT, C_NEED, C_SEEDS, C_GOT = range(1, 7)


class SynthPage(StepPage):
    title = "Synth"

    def __init__(self, handle, parent: Optional[QWidget] = None):
        super().__init__(handle, parent)
        self._stats: dict = {}
        self._seeds: dict = {}
        self._result: dict = {}
        _, root = page_frame(self, "Synthetic data",
                             "Real samples are uneven: some digits turn up far more than others. "
                             "This renders extra training images for the weak characters so every "
                             "class reaches the target. Real, approved glyphs always count first; "
                             "approved font renders come after them, and the stock templates "
                             "only stand in for a character that has neither.")

        card = Card(self, "Settings")
        row = QHBoxLayout()
        row.setSpacing(10)
        row.addWidget(self._cap("Region kind", card))
        self.kind = QComboBox(card)
        self.kind.setStyleSheet(COMBO_QSS)
        for k in KINDS:
            self.kind.addItem(KIND_LABELS[k], k)
        self.kind.setCurrentIndex(KINDS.index("signal_rgb"))
        self.kind.currentIndexChanged.connect(lambda _i: self.refresh())
        row.addWidget(self.kind)
        row.addSpacing(16)
        row.addWidget(self._cap("Target per character", card))
        self.per_class = QSpinBox(card)
        self.per_class.setRange(10, 5000)
        self.per_class.setSingleStep(10)
        self.per_class.setValue(150)
        self.per_class.setFixedWidth(100)
        self.per_class.setStyleSheet(
            f"QSpinBox {{ background: rgba(28, 34, 51, 200); color: {P.fg_bright}; "
            f"border: 1px solid {P.border_card}; border-radius: 3px; padding: 4px 6px; "
            f"font-family: {MONO}; font-size: 10pt; }}")
        self.per_class.valueChanged.connect(lambda _v: self._fill())
        row.addWidget(self.per_class)
        row.addStretch(1)
        self.gen_btn = button("Generate", card, "primary")
        self.gen_btn.clicked.connect(self.generate)
        row.addWidget(self.gen_btn)
        card.body.addLayout(row)
        self.bar = progress_bar(card)
        card.body.addWidget(self.bar)
        self.status = status_label(card)
        card.body.addWidget(self.status)
        root.addWidget(card)

        tcard = Card(self, "Per character")
        self.table = QTableWidget(0, len(COLS), tcard)
        self.table.setHorizontalHeaderLabels(COLS)
        self.table.setStyleSheet(TABLE_QSS)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionMode(QAbstractItemView.NoSelection)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(C_SEEDS, QHeaderView.ResizeToContents)
        self.table.verticalHeader().setDefaultSectionSize(26)
        tcard.body.addWidget(self.table, 1)
        root.addWidget(tcard, 1)

    def _cap(self, text: str, parent) -> QLabel:
        lbl = QLabel(text.upper(), parent)
        lbl.setStyleSheet(f"color: {P.fg_dim}; font-family: {HEAD}; font-size: 8pt; "
                          f"background: transparent;")
        return lbl

    def refresh(self) -> None:
        self._result = {}
        self.runner.start("Counting glyphs", self._fetch, self.kind.currentData(),
                          on_done=self._got_stats)

    def _fetch(self, kind: str) -> dict:
        seeds_fn = getattr(self.api, "synth_seeds", None)
        return {"stats": self.api.glyph_stats(kind),
                "seeds": seeds_fn(kind) if callable(seeds_fn) else {}}

    def _got_stats(self, data: dict) -> None:
        self._stats = dict((data or {}).get("stats") or {})
        self._seeds = dict((data or {}).get("seeds") or {})
        self._fill()

    def _fill(self) -> None:
        target = self.per_class.value()
        chars = list(self._stats.keys()) or list(self._result.keys())
        self.table.setRowCount(len(chars))
        total = 0
        for r, ch in enumerate(chars):
            s = self._stats.get(ch, {})
            appr, pend = int(s.get("approved", 0)), int(s.get("pending", 0))
            real, font = _src(s, "capture", "approved"), _src(s, "font", "approved")
            need = max(0, target - appr)
            total += need
            got = self._result.get(ch)
            sd = self._seeds.get(ch) or {}
            if sd.get("real") or sd.get("font"):
                seeds = f"{int(sd.get('real', 0)):,} real / {int(sd.get('font', 0)):,} font"
            elif sd.get("stock"):
                seeds = "stock template"
            else:
                seeds = "none" if self._seeds else "—"
            vals = (f"{ch}   ({_char_name(ch)})" if _char_name(ch) != ch else ch,
                    f"{real:,}", f"{font:,}", f"{pend:,}", f"{need:,}", seeds,
                    "—" if got is None else f"{got:,}")
            for c, v in enumerate(vals):
                it = QTableWidgetItem(v)
                it.setTextAlignment(Qt.AlignCenter)
                if c == C_REAL and real < WEAK:
                    it.setForeground(QColor(P.red if appr < WEAK else FONT_COLOUR))
                    it.setToolTip(f"Under {WEAK} real samples"
                                  + ("" if appr < WEAK else "; topped up by font renders"))
                if c == C_FONT and font:
                    it.setForeground(QColor(FONT_COLOUR))
                    it.setToolTip("Rendered from the game font: training only, never benchmarked")
                if c == C_NEED and need:
                    it.setForeground(QColor(P.yellow))
                if c == C_SEEDS:
                    it.setToolTip("What the synthetic images grow from: approved real glyphs "
                                  "first, then approved font renders; stock templates only "
                                  "when a character has neither.")
                    if seeds in ("stock template", "none"):
                        it.setForeground(QColor(P.fg_dim))
                if c == C_GOT and got:
                    it.setForeground(QColor(ACCENT))
                self.table.setItem(r, c, it)
        if not self.runner.is_running("Generating synthetic data"):
            if not chars:
                self.status.setText("No approved glyphs for this kind yet (step 4). Synthetic "
                                    "images are modelled on real ones, so approve some first.")
            elif not self._result:
                self.status.setText(f"Will render {total:,} images to bring every character up "
                                    f"to {target:,}.")
        self.gen_btn.setEnabled(bool(chars))

    def generate(self) -> None:
        kind, target = self.kind.currentData(), self.per_class.value()
        self.run_long("Generating synthetic data", self.api.generate_synth, kind, target,
                      bar=self.bar, status=self.status,
                      disable=(self.gen_btn, self.kind, self.per_class),
                      on_done=self._generated)

    def _generated(self, result: dict) -> None:
        self._result = {k: int(v) for k, v in (result or {}).items()}
        n = sum(self._result.values())
        self.status.setText(f"Generated {n:,} images across {len(self._result)} characters.")
        self.set_summary(f"+{n:,} images")
        self._fill()
