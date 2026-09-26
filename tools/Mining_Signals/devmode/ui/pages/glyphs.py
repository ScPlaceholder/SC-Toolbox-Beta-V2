"""Step 4 — approve or reject the single-character glyphs cut from
confirmed captures, one character at a time, with weak classes flagged."""

from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QHBoxLayout, QLabel, QListView, QListWidget,
    QListWidgetItem, QPushButton, QButtonGroup, QVBoxLayout, QWidget,
)

from shared.qt.theme import P

from ..style import (
    ACCENT, COMBO_QSS, HEAD, KIND_LABELS, KINDS, MONO, Card, button, heading, subtext,
)
from .base import StepPage, load_qimage, progress_bar, scaled_pixmap, status_label, status_style

# Below this many approved samples a character class is "weak". Matches the
# training registry's floor_per_class default (ocr/training_registry.py).
WEAK = 30
ICON = QSize(56, 56)
SHOW = (("Waiting for review", "pending"), ("Approved", "approved"), ("Rejected", "rejected"),
        ("All", None))


def _char_button(ch: str, approved: int, pending: int, weak: bool) -> QPushButton:
    """Two-line toggle: the character large, its counts small underneath."""
    colour = P.red if weak else P.fg
    counts = f"{approved}✓ {pending}?" if pending else f"{approved}✓"
    b = QPushButton(f"{ch}\n{counts}")
    b.setCheckable(True)
    b.setCursor(Qt.PointingHandCursor)
    b.setFixedSize(64, 48)
    tip = f"'{_char_name(ch)}': {approved} approved, {pending} waiting for review"
    b.setToolTip(tip + (f"\nWEAK: under {WEAK} approved samples" if weak else ""))
    b.setStyleSheet(
        f"QPushButton {{ background: {P.bg_card}; color: {colour}; border: 1px solid "
        f"{P.red if weak else P.border_card}; border-radius: 3px; padding: 2px; "
        f"font-family: {MONO}; font-size: 9pt; font-weight: bold; min-height: 0px; }}"
        f"QPushButton:hover {{ border-color: {ACCENT}; }}"
        f"QPushButton:checked {{ background: rgba(51, 221, 136, 45); border: 2px solid {ACCENT}; "
        f"color: {P.fg_bright if not weak else P.red}; }}")
    return b


def _char_name(ch: str) -> str:
    return {",": "comma", ".": "dot", "%": "percent", " ": "space"}.get(ch, ch)


class GlyphsPage(StepPage):
    title = "Glyphs"

    def __init__(self, handle, parent: Optional[QWidget] = None):
        super().__init__(handle, parent)
        self._stats: dict = {}
        self._chars: list[str] = []
        self._rows: list[dict] = []
        self._note = ""

        root = QVBoxLayout(self)
        root.setContentsMargins(22, 16, 22, 12)
        root.setSpacing(10)

        top = QHBoxLayout()
        top.setSpacing(10)
        top.addWidget(heading("Glyphs", self))
        top.addSpacing(12)
        self.kind = QComboBox(self)
        self.kind.setStyleSheet(COMBO_QSS)
        for k in KINDS:
            self.kind.addItem(KIND_LABELS[k], k)
        self.kind.setCurrentIndex(KINDS.index("signal_rgb"))
        self.kind.currentIndexChanged.connect(lambda _i: self.refresh())
        top.addWidget(self.kind)
        self.show_combo = QComboBox(self)
        self.show_combo.setStyleSheet(COMBO_QSS)
        for text, val in SHOW:
            self.show_combo.addItem(text, val)
        self.show_combo.currentIndexChanged.connect(lambda _i: self._load_char())
        top.addWidget(self.show_combo)
        top.addStretch(1)
        self.extract_btn = button("Cut glyphs from confirmed captures", self, "primary",
                                  "Splits every confirmed crop of this kind into single "
                                  "characters and queues them here for review.")
        self.extract_btn.clicked.connect(self.extract)
        top.addWidget(self.extract_btn)
        root.addLayout(top)

        root.addWidget(subtext(
            f"Each button is one character. Red ones are weak: fewer than {WEAK} approved samples, "
            "so the model sees too few real examples of it. Reject anything cut badly (two "
            "digits, half a digit, wrong character).", self))
        self.bar = progress_bar(self)
        root.addWidget(self.bar)
        self.status = status_label(self)
        root.addWidget(self.status)

        card = Card(self, margins=(10, 8, 10, 10))
        root.addWidget(card, 1)
        # One button per character (not a QTabBar: tab colours can't be set
        # per tab under the toolbox stylesheet, and weak classes must stand out).
        self.char_row = QHBoxLayout()
        self.char_row.setSpacing(4)
        self.char_row.addStretch(1)
        card.body.addLayout(self.char_row)
        self.char_group = QButtonGroup(self)
        self.char_group.setExclusive(True)
        self.char_group.idClicked.connect(lambda _i: self._load_char())
        self.char_buttons: list[QPushButton] = []

        self.grid = QListWidget(card)
        self.grid.setViewMode(QListView.IconMode)
        self.grid.setIconSize(ICON)
        self.grid.setGridSize(QSize(ICON.width() + 12, ICON.height() + 12))
        self.grid.setMovement(QListView.Static)
        self.grid.setResizeMode(QListView.Adjust)
        self.grid.setUniformItemSizes(True)
        self.grid.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.grid.setStyleSheet(
            f"QListWidget {{ background: rgba(5, 7, 10, 200); border: 1px solid {P.border}; }}"
            f"QListWidget::item {{ border: 2px solid transparent; border-radius: 3px; }}"
            f"QListWidget::item:selected {{ background: rgba(51, 221, 136, 60); "
            f"border: 2px solid {ACCENT}; }}")
        self.grid.itemSelectionChanged.connect(self._sel_changed)
        card.body.addWidget(self.grid, 1)

        brow = QHBoxLayout()
        brow.setSpacing(8)
        self.approve_btn = button("✓  Approve selected  [A]", card, "primary")
        self.approve_btn.clicked.connect(self.approve_selected)
        self.reject_btn = button("✕  Reject selected  [X]", card, "danger")
        self.reject_btn.clicked.connect(self.reject_selected)
        self.all_btn = button("Select all  [Ctrl+A]", card)
        self.all_btn.clicked.connect(self.grid.selectAll)
        for b in (self.approve_btn, self.reject_btn, self.all_btn):
            brow.addWidget(b)
        brow.addStretch(1)
        self.counts = QLabel("", card)
        self.counts.setStyleSheet(status_style(P.fg_dim))
        brow.addWidget(self.counts)
        card.body.addLayout(brow)

        for keys, slot in (("A", self.approve_selected), ("X", self.reject_selected),
                           ("Delete", self.reject_selected)):
            sc = QShortcut(QKeySequence(keys), self)
            sc.setContext(Qt.WidgetWithChildrenShortcut)
            sc.activated.connect(slot)
        self._sel_changed()

    # ── data ──
    @property
    def current_kind(self) -> str:
        return self.kind.currentData()

    @property
    def current_char(self) -> Optional[str]:
        i = self.char_group.checkedId()
        return self._chars[i] if 0 <= i < len(self._chars) else None

    def set_char(self, ch: str) -> None:
        if ch in self._chars:
            self.char_buttons[self._chars.index(ch)].setChecked(True)
            self._load_char()

    def refresh(self) -> None:
        self.runner.start("Counting glyphs", self.api.glyph_stats, self.current_kind,
                          on_done=self._got_stats)

    def _got_stats(self, stats: dict) -> None:
        self._stats = dict(stats or {})
        keep = self.current_char
        for b in self.char_buttons:
            self.char_group.removeButton(b)
            b.deleteLater()
        self.char_buttons = []
        self._chars = list(self._stats.keys())
        for i, ch in enumerate(self._chars):
            s = self._stats[ch]
            appr, pend = int(s.get("approved", 0)), int(s.get("pending", 0))
            b = _char_button(ch, appr, pend, appr < WEAK)
            self.char_group.addButton(b, i)
            self.char_row.insertWidget(i, b)
            self.char_buttons.append(b)
        if self._chars:
            pick = keep if keep in self._chars else next(
                (c for c in self._chars if int(self._stats[c].get("pending", 0))), self._chars[0])
            self.char_buttons[self._chars.index(pick)].setChecked(True)
        weak = [c for c in self._chars if int(self._stats[c].get("approved", 0)) < WEAK]
        pending = sum(int(s.get("pending", 0)) for s in self._stats.values())
        self.set_summary(f"{pending:,} waiting" if pending else
                         (f"{len(weak)} weak" if weak else "all approved"))
        if not self._chars:
            self.status.setText("No glyphs for this kind yet. Confirm some captures in step 3, "
                                "then cut glyphs from them.")
        else:
            self.status.setText(
                self._note + f"{len(self._chars)} characters · {pending:,} waiting for review · weak: "
                + (", ".join(_char_name(c) for c in weak) if weak else "none"))
        self._note = ""
        self._load_char()

    def _load_char(self) -> None:
        ch = self.current_char
        if ch is None:
            self.grid.clear()
            self._rows = []
            self._sel_changed()
            return
        self.runner.start("Loading glyphs", self._fetch, self.current_kind, ch,
                          self.show_combo.currentData(), on_done=self._got_rows)

    def _fetch(self, kind: str, ch: str, status: Optional[str]) -> dict:
        rows = []
        for r in self.api.list_glyphs(kind, char=ch, status=status):
            d = dict(r)
            d["_img"] = load_qimage(d.get("image_path"))
            rows.append(d)
        return {"char": ch, "rows": rows}

    def _got_rows(self, data: dict) -> None:
        if data["char"] != self.current_char:
            return                       # the user already moved to another tab
        self._rows = data["rows"]
        self.grid.clear()
        for r in self._rows:
            li = QListWidgetItem()
            li.setIcon(scaled_pixmap(r.get("_img"), ICON.width(), ICON.height()))
            li.setData(Qt.UserRole, r["id"])
            li.setToolTip(f"{r['id']} · {r.get('status')}")
            self.grid.addItem(li)
        self._sel_changed()

    def _sel_changed(self) -> None:
        n = len(self.grid.selectedItems())
        self.approve_btn.setEnabled(n > 0)
        self.reject_btn.setEnabled(n > 0)
        self.all_btn.setEnabled(self.grid.count() > 0)
        ch = self.current_char
        self.counts.setText(f"'{_char_name(ch)}': {self.grid.count()} shown · {n} selected"
                            if ch is not None else "")

    # ── actions ──
    def extract(self) -> None:
        kind = self.current_kind
        self.run_long("Cutting glyphs", self.api.extract_glyphs, kind,
                      bar=self.bar, status=self.status, disable=(self.extract_btn, self.kind),
                      on_done=lambda n: self._extracted(n))

    def _extracted(self, n: int) -> None:
        self._note = f"Cut {n:,} new glyphs. "
        self.refresh()

    def approve_selected(self) -> None:
        self._decide(self.api.approve_glyph, "Approving glyphs")

    def reject_selected(self) -> None:
        self._decide(self.api.reject_glyph, "Rejecting glyphs")

    def _decide(self, fn, name: str) -> None:
        items = self.grid.selectedItems()
        ids = [li.data(Qt.UserRole) for li in items]
        if not ids:
            return
        for li in items:                 # optimistic: drop them from view now
            self.grid.takeItem(self.grid.row(li))
        self._sel_changed()

        def work(ids=ids):
            for gid in ids:
                fn(gid)
            return len(ids)

        self.runner.start(name, work, on_done=lambda _n: self.refresh(),
                          on_error=lambda m: (self.status.setText(f"{name} failed: {m}"),
                                              self.refresh()))
